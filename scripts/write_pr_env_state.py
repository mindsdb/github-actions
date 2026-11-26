"""Record a PR environment's deploy in mindsdb/argocd-state.

A PR environment's parent Application reads ``pr-envs/<env>.yaml`` from that
repository as its values file (argocd-state's README has the keys). After a
build, the anchor's deploy job writes what it resolved for this run: ``tags``,
the image tag per repository, and ``revisions``, the chart commit or branch
per repository. Both maps are replaced whole, so a ``Deploys:`` link removed
from the PR drops its pin on the next deploy, and the staff console (which the
parent renders only while both of its keys are set) goes with its line.
Everything else in the file stays: ``services.apiHost``, which the
mindshub_services stacks workflow writes with ``--api-host`` (that mode
touches nothing else), and ``hostBase``, which only a customer environment's
hand-written file carries.

The file is created when it does not exist: the parent does not render
without it, and this is the deploy that gives the environment its first
images. The ``argocd-pr-env-deploy`` action calls this; the push is retried
through ``write_env_state.push_change`` when another deploy lands first.

    write_pr_env_state.py --repo-dir <clone> --env pr-cowork-server-12 \
        --set tags.cowork-server=development-<sha> --set revisions.cowork-server=<sha>
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import yaml

from write_env_state import TAG, StateError, push_change

# The same shapes argocd-state's validate.py accepts for pr-envs/.
ENV = re.compile(r"^pr-[a-z0-9-]+-[1-9][0-9]*$")
REPO_KEY = re.compile(r"^[a-z0-9_.-]+$")
REVISION = re.compile(r"^([0-9a-f]{40}|staging|main)$")
HOSTNAME = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)*$")
MAPS = ("tags", "revisions")
# The maps CI replaces come first; what the file keeps from others follows in its own order.
ORDER = ("hostBase", "tags", "revisions", "services")


def parse_settings(settings: list[str]) -> dict[str, dict[str, str]]:
    """``tags.<repo>=<tag>`` and ``revisions.<repo>=<revision>`` lines as the two maps, each value checked."""
    out: dict[str, dict[str, str]] = {name: {} for name in MAPS}
    for setting in settings:
        key, sep, value = setting.partition("=")
        name, dot, repo = key.partition(".")
        if not sep or not dot or name not in MAPS:
            raise StateError(f"{setting!r} is not tags.<repo>=<tag> or revisions.<repo>=<revision>")
        if not REPO_KEY.match(repo):
            raise StateError(f"{repo!r} is not a repository short name")
        shape = TAG if name == "tags" else REVISION
        if not shape.match(value):
            raise StateError(f"{key}={value!r} is not {'an image tag' if name == 'tags' else 'a commit sha, staging or main'}")
        if repo in out[name]:
            raise StateError(f"{key} is set twice")
        out[name][repo] = value
    return out


def updated(data: dict, settings: dict[str, dict[str, str]] | None = None, api_host: str | None = None) -> dict:
    """The file's mapping with the two maps replaced, or services.apiHost set, and every other key kept."""
    new = dict(data)
    if settings is not None:
        new.update({name: dict(values) for name, values in settings.items()})
        for name in MAPS:
            if not new[name]:
                del new[name]
    if api_host is not None:
        new["services"] = {**(new.get("services") or {}), "apiHost": api_host}
    return {key: new[key] for key in ORDER if key in new} | {key: value for key, value in new.items() if key not in ORDER}


def load(text: str) -> dict:
    data = yaml.safe_load(text) if text.strip() else {}
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise StateError("the file is not a mapping")
    return data


def record(repo_dir: Path, branch: str, env: str, settings: list[str] | None = None, attempts: int = 5, *,
           api_host: str | None = None) -> tuple[bool, str]:
    """Write the environment's maps, or its API host, and push. Returns whether a commit was pushed and a message for the job log."""
    if not ENV.match(env):
        raise StateError(f"env {env!r} is not pr-<anchor>-<number>")
    if (settings is None) == (api_host is None):
        raise StateError("either --set lines or --api-host, not both and not neither")
    if api_host is not None and not HOSTNAME.match(api_host):
        raise StateError(f"api host {api_host!r} is not a hostname")
    parsed = parse_settings(settings) if settings is not None else None
    path = Path("pr-envs") / f"{env}.yaml"

    def edit(root: Path) -> str | None:
        file = root / path
        text = file.read_text(encoding="utf-8") if file.is_file() else ""
        if api_host is not None and not file.is_file():
            # The stack came up before the environment's first deploy wrote
            # the file; a file holding only the host would be an environment
            # with no images. The next reconcile writes it.
            raise StateError(f"{path} does not exist yet: the environment's deploy writes it first")
        new = updated(load(text), parsed, api_host)
        dumped = yaml.safe_dump(new, sort_keys=False, default_flow_style=False)
        if file.is_file() and load(text) == new:
            return None
        file.parent.mkdir(exist_ok=True)
        file.write_text(dumped, encoding="utf-8")
        if parsed is None:
            return f"{path}: services.apiHost={api_host}"
        return f"{path}: " + ", ".join(f"{name}.{repo}={value}" for name in MAPS for repo, value in parsed[name].items())

    if parsed is None:
        subject = f"{env}: services.apiHost {api_host}"
    else:
        subject = f"{env}: " + ", ".join(parsed["tags"].values() or parsed["revisions"].values())
    pushed, message = push_change(repo_dir, branch, edit, subject, attempts)
    if not pushed:
        return False, f"{path} already records this"
    return True, message


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("--repo-dir", type=Path, required=True, help="a clone of argocd-state with origin set")
    parser.add_argument("--branch", default="main")
    parser.add_argument("--env", required=True, help="the environment, pr-<anchor>-<number>")
    parser.add_argument("--set", action="append", metavar="KEY=VALUE", help="tags.<repo>=<tag> or revisions.<repo>=<revision>; repeat")
    parser.add_argument("--api-host", metavar="HOST", help="set services.apiHost alone, as the stacks workflow does")
    parser.add_argument("--attempts", type=int, default=5)
    parser.add_argument("--github-output", type=Path, help="a file to append pushed=true|false to, as $GITHUB_OUTPUT")
    args = parser.parse_args(argv)
    try:
        pushed, message = record(args.repo_dir, args.branch, args.env, args.set, args.attempts, api_host=args.api_host)
    except StateError as error:
        print(f"::error::{error}")
        return 1
    print(message)
    if args.github_output:
        with args.github_output.open("a", encoding="utf-8") as out:
            out.write(f"pushed={'true' if pushed else 'false'}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
