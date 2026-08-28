"""Guards on ``cla-assistant.yml``'s allowlist default.

The allowlist decides who is exempt from signing the contributor agreement, so a
wrong entry is a legal gap rather than a broken build, and nothing else fails
when it drifts. Three real defects sat in it across fifteen repos before anyone
looked, and each one is pinned below.

The matching logic these tests encode lives in the action, not here:
``checkAllowList.ts`` compiles an entry containing ``*`` to
``new RegExp(escaped).test(committer)`` and compares every other entry with
``pattern === committer``. Both halves matter, and both are quoted in the tests
that depend on them.
"""

import re
from pathlib import Path

import pytest
import yaml

_WORKFLOW = (
    Path(__file__).resolve().parents[1] / ".github" / "workflows" / "cla-assistant.yml"
)

# github-actions[bot] never reaches the allowlist: the action drops user id
# 41898282 in graphql.ts before the check runs, so an entry for it is dead
# weight that reads like a decision.
FILTERED_UPSTREAM = "github-actions[bot]"


def allowlist_default() -> str:
    spec = yaml.safe_load(_WORKFLOW.read_text(encoding="utf-8"))
    # PyYAML resolves the bare `on:` key to True, so accept either spelling.
    triggers = spec.get("on", spec.get(True))
    return triggers["workflow_call"]["inputs"]["allowlist"]["default"]


def entries() -> list[str]:
    return [e.strip() for e in allowlist_default().split(",") if e.strip()]


def test_the_default_exists_so_no_caller_has_to_pass_one():
    """A caller passing its own list is how six copies drifted apart."""
    assert allowlist_default()


def test_no_wildcards():
    """`bot*` becomes an UNANCHORED `bot.*`, so it also exempts `robotnik`.

    `checkAllowList.ts` builds `new RegExp(pattern.replace('*', '.*'))` and calls
    `.test()`, which searches anywhere in the string. Anyone could opt out of the
    CLA by picking a username containing the pattern.
    """
    assert [e for e in entries() if "*" in e] == []


def test_every_entry_is_a_bot():
    """People sign. A list of people is an access-control list nobody prunes.

    Eleven of the twenty-five names in the old per-repo lists had already left
    the org and were still exempt.
    """
    assert [e for e in entries() if not e.endswith("[bot]")] == []


def test_github_actions_bot_is_not_listed():
    assert FILTERED_UPSTREAM not in entries()


def test_no_duplicates():
    assert len(entries()) == len(set(entries()))


@pytest.mark.parametrize(
    "impostor", ["robotnik", "sabotage", "elliotbotson", "dependabot-impostor"]
)
def test_the_default_does_not_exempt_a_lookalike(impostor):
    """Replays the action's own matcher against logins a wildcard would have let through."""

    def action_matches(pattern: str, committer: str) -> bool:
        pattern = pattern.strip()
        if "*" in pattern:
            return re.search(re.escape(pattern).replace(r"\*", ".*"), committer) is not None
        return pattern == committer

    assert not any(action_matches(e, impostor) for e in entries())


@pytest.mark.parametrize("bot", ["dependabot[bot]", "mindsdb-release-train[bot]"])
def test_the_bots_that_actually_open_pull_requests_stay_exempt(bot):
    """A bot cannot post the agreement sentence, so dropping it means a permanent red check."""
    assert bot in entries()
