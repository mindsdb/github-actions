"""Delete the GitHub Environments left behind by closed PR environments.

Each anchor repo's deploy job runs in a GitHub Environment named
``pr-<repo slug>-<PR number>``, which GitHub creates on first use and never
removes. The PR env itself lives exactly as long as the PR is open and carries
the ``deploy`` label (the PR-env ApplicationSet's filter), so an environment
whose PR fails that test is stale and is deleted here.

Environments are listed before PRs, so an environment created mid-run is left
for the next run. A repo whose PR list cannot be read is skipped entirely.

With ``--state-dir``, a clone of mindsdb/argocd-state, the same test deletes
the environment's values file, ``pr-envs/pr-<repo slug>-<PR number>.yaml``,
which the anchor's deploy job wrote and nothing else removes. A file whose
``hostBase`` is set is a customer environment's, written and removed by hand,
and is left alone. The deletions go in one commit, pushed with the same retry
as a deploy's write.
Each read retries up to three attempts, since one truncated ``--paginate`` page
fails the call; writes run once, so a reminder is never posted twice.

A closed PR labelled ``deploy`` and ``customer:<hostBase>`` had a customer env
whose data nothing deletes, so each one gets a single comment listing it (see
``REMINDER``). Every closed PR is scanned, so a PR missed while the job was
failing is still reminded once it recovers.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable, Iterable, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent))
from write_env_state import StateError, push_change  # noqa: E402

Runner = Callable[[Sequence[str]], "subprocess.CompletedProcess[str]"]
Sleep = Callable[[float], None]

DEPLOY_LABEL = "deploy"
CUSTOMER_PREFIX = "customer:"
MARKER = "<!-- customer-env-close-reminder -->"
REMINDER = MARKER + """
This customer env is gone, but its data is not. Delete by hand when the engagement is over:
- Files: the `customer-envs/{host_base}/` prefix in the bucket named by `filesBucket` in argocd-envs `parent/values.yaml`
- Cowork workspaces: the `pr-{slug}-{number}` directory on the cowork EFS
- Postgres: the retained PersistentVolume for `data-postgres-postgresql-0` in namespace `pr-{slug}-{number}` (`kubectl get pv | grep pr-{slug}-{number}`)
- SAM stack: the `anton-services-pr-{slug}-{number}` CloudFormation stack and its DynamoDB tables, if it still exists
"""


def _run(argv: Sequence[str]) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(list(argv), capture_output=True, text=True, check=False)


def _read(runner: Runner, argv: Sequence[str], *, sleep: Sleep, attempts: int = 3) -> "subprocess.CompletedProcess[str]":
    """Run a read-only gh call, retrying a failure after 2 s, then 4 s; returns the last result."""
    result = runner(argv)
    for attempt in range(1, attempts):
        if result.returncode == 0:
            break
        sleep(2 * attempt)
        result = runner(argv)
    return result


def stale_environments(repo: str, names: Iterable[str], keep: set[int]) -> list[str]:
    """PR-env environment names in ``repo`` whose PR number is not in ``keep``."""
    slug = repo.split("/", 1)[1].replace("_", "-")
    pattern = re.compile(rf"^pr-{re.escape(slug)}-([0-9]+)$")
    stale = []
    for name in names:
        match = pattern.match(name)
        if match and int(match.group(1)) not in keep:
            stale.append(name)
    return sorted(stale)


def stale_state_files(state_dir: Path, repo: str, keep: set[int]) -> list[Path]:
    """The files under pr-envs/ for ``repo``'s PR envs whose PR is not in ``keep``, customer environments left out."""
    names = {path.stem: path for path in (state_dir / "pr-envs").glob("pr-*.yaml")}
    return [names[name] for name in stale_environments(repo, names, keep) if not _host_base(names[name])]


def _host_base(path: Path) -> str:
    """The file's hostBase, read without a YAML parser: CI writes none, and a hand-written customer file sets it on its own line."""
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("hostBase:"):
            return line.split(":", 1)[1].split("#", 1)[0].strip().strip("'\"")
    return ""


def delete_state_files(state_dir: Path, paths: Sequence[Path], *, dry_run: bool) -> bool:
    """Delete these files from argocd-state in one pushed commit; False if the push failed."""
    names = sorted(path.stem for path in paths)
    if dry_run:
        for name in names:
            print(f"  would delete pr-envs/{name}.yaml", flush=True)
        return True

    def edit(root: Path) -> str | None:
        gone = [name for name in names if (root / "pr-envs" / f"{name}.yaml").is_file()]
        for name in gone:
            (root / "pr-envs" / f"{name}.yaml").unlink()
        return f"deleted {', '.join(gone)}" if gone else None

    try:
        _pushed, message = push_change(state_dir, "main", edit, "prune: " + ", ".join(names))
    except StateError as error:
        print(f"::error title=argocd-state::{error}", flush=True)
        return False
    print(f"argocd-state: {message}", flush=True)
    return True


def _lines(result: "subprocess.CompletedProcess[str]") -> list[str]:
    return [line for line in result.stdout.splitlines() if line.strip()]


def _error(result: "subprocess.CompletedProcess[str]") -> str:
    return result.stderr.strip() or result.stdout.strip()


def customer_reminders(repo: str, runner: Runner = _run, *, dry_run: bool, sleep: Sleep = time.sleep) -> bool:
    """Comment once on each closed PR in ``repo`` that had a customer env; False if a gh call failed."""
    slug = repo.split("/", 1)[1].replace("_", "-")
    closed = _read(runner, [
        "gh", "api", "--paginate", f"repos/{repo}/pulls?state=closed&per_page=100",
        "--jq", f'.[] | select(any(.labels[]; .name == "{DEPLOY_LABEL}")) | '
                f'select(any(.labels[]; .name | startswith("{CUSTOMER_PREFIX}"))) | '
                f'"\\(.number) \\([.labels[].name | select(startswith("{CUSTOMER_PREFIX}"))][0])"',
    ], sleep=sleep)
    if closed.returncode != 0:
        print(f"::error title={repo}::could not list closed PRs: {_error(closed)}", flush=True)
        return False
    ok = True
    for line in _lines(closed):
        number, label = line.split(" ", 1)
        comments = _read(runner, ["gh", "api", "--paginate", f"repos/{repo}/issues/{number}/comments", "--jq", ".[].body"], sleep=sleep)
        if comments.returncode != 0:
            print(f"::error title={repo}::could not read comments on #{number}: {_error(comments)}", flush=True)
            ok = False
            continue
        if MARKER in comments.stdout:
            continue
        if dry_run:
            print(f"  would remind on {repo}#{number}", flush=True)
            continue
        body = REMINDER.format(host_base=label.removeprefix(CUSTOMER_PREFIX), slug=slug, number=number)
        res = runner(["gh", "api", "-X", "POST", f"repos/{repo}/issues/{number}/comments", "-f", f"body={body}"])
        if res.returncode != 0:
            print(f"::error title={repo}::could not comment on #{number}: {_error(res)}", flush=True)
            ok = False
        else:
            print(f"  reminded {repo}#{number}", flush=True)
    return ok


def prune(repos: Sequence[str], *, dry_run: bool, runner: Runner = _run, sleep: Sleep = time.sleep, state_dir: Path | None = None) -> int:
    failed = False
    stale_files: list[Path] = []
    for repo in repos:
        envs = _read(runner, ["gh", "api", "--paginate", f"repos/{repo}/environments?per_page=100", "--jq", ".environments[].name"], sleep=sleep)
        if envs.returncode != 0:
            print(f"::error title={repo}::could not list environments: {_error(envs)}", flush=True)
            failed = True
            continue
        prs = _read(runner, [
            "gh", "api", "--paginate", f"repos/{repo}/pulls?state=open&per_page=100",
            "--jq", f'.[] | select(any(.labels[]; .name == "{DEPLOY_LABEL}")) | .number',
        ], sleep=sleep)
        if prs.returncode != 0:
            print(f"::error title={repo}::could not list PRs, deleting nothing: {_error(prs)}", flush=True)
            failed = True
            continue
        keep = {int(n) for n in _lines(prs)}
        stale = stale_environments(repo, _lines(envs), keep)
        if state_dir is not None:
            stale_files += stale_state_files(state_dir, repo, keep)
        print(f"{repo}: {len(stale)} stale", flush=True)
        for name in stale:
            if dry_run:
                print(f"  would delete {name}", flush=True)
                continue
            res = runner(["gh", "api", "-X", "DELETE", f"repos/{repo}/environments/{name}"])
            if res.returncode != 0:
                print(f"::error title={repo}::could not delete {name}: {_error(res)}", flush=True)
                failed = True
            else:
                print(f"  deleted {name}", flush=True)
        if not customer_reminders(repo, runner, dry_run=dry_run, sleep=sleep):
            failed = True
    if stale_files:
        print(f"argocd-state: {len(stale_files)} stale", flush=True)
        if not delete_state_files(state_dir, stale_files, dry_run=dry_run):
            failed = True
    return 1 if failed else 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--repo", action="append", required=True, metavar="OWNER/NAME", help="anchor repo; repeat for each")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--state-dir", type=Path, help="a clone of argocd-state with origin set; stale pr-envs/ files are deleted from it")
    args = parser.parse_args(argv)
    return prune(args.repo, dry_run=args.dry_run, state_dir=args.state_dir)


if __name__ == "__main__":
    sys.exit(main())
