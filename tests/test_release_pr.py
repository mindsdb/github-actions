"""Run the release-body step without GitHub access or repository credentials."""

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
MERGED = "e" * 40


def run_release_step(tmp_path, *, response, api_exit=0, ancestor=0, existing="7"):
    workflow = yaml.safe_load((ROOT / ".github/workflows/release-pr.yml").read_text())
    steps = [step for step in workflow["jobs"]["create-pr"]["steps"]
             if step.get("name") == "Create or update PR"]
    assert len(steps) == 1
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (tmp_path / "response.json").write_text(response)
    gh = bin_dir / "gh"
    gh.write_text(f"#!{sys.executable}\n" + '''
import json, os, sys
from pathlib import Path
args = sys.argv[1:]
with Path("gh-calls.jsonl").open("a") as handle:
    handle.write(json.dumps(args) + "\\n")
if args[0] == "api":
    sys.stdout.write(Path("response.json").read_text())
    sys.exit(int(os.environ["API_EXIT"]))
elif args[:2] == ["pr", "list"]:
    print(os.environ["EXISTING"])
elif args[:2] == ["pr", "view"]:
    print("true")
elif args[:2] in (["pr", "create"], ["pr", "edit"]):
    Path("body.md").write_text(args[args.index("--body") + 1])
    print("https://github.com/o/r/pull/7")
else:
    raise AssertionError(args)
''')
    git = bin_dir / "git"
    git.write_text(f"#!{sys.executable}\n" + '''
import json, os, sys
from pathlib import Path
args = sys.argv[1:]
with Path("git-calls.jsonl").open("a") as handle:
    handle.write(json.dumps(args) + "\\n")
if args[:2] == ["merge-base", "--is-ancestor"]:
    assert args[2:] == [os.environ["MERGED"], "origin/staging"]
    sys.exit(int(os.environ["ANCESTOR"]))
elif args == ["log", "--format=%an", "origin/main..origin/staging"]:
    print("Synthetic contributor")
elif args[:3] == ["log", "--oneline", "--no-merges"]:
    if args[3:] == ["origin/main..origin/staging"]:
        print("old zqcanary7731 carried subject")
    else:
        assert args[3:] == ["origin/staging", "^" + os.environ["MERGED"], "^origin/main"]
    print("new clean subject")
else:
    raise AssertionError(args)
''')
    gh.chmod(0o755)
    git.chmod(0o755)
    jq = shutil.which("jq")
    bash = shutil.which("bash")
    assert jq and bash, "release workflow tests require jq and bash"
    result = subprocess.run(
        [bash, "--noprofile", "--norc", "-c", steps[0]["run"]],
        cwd=tmp_path, text=True, capture_output=True, timeout=15,
        env={
            "PATH": f"{bin_dir}:{Path(jq).parent}:/usr/bin:/bin",
            "REPO": "o/r", "BASE": "main", "STAGING": "staging", "AHEAD": "2",
            "FROZEN": "false", "MERGED": MERGED, "API_EXIT": str(api_exit),
            "ANCESTOR": str(ancestor), "EXISTING": existing,
        },
    )
    calls = [json.loads(line) for line in (tmp_path / "gh-calls.jsonl").read_text().splitlines()]
    return result, calls


@pytest.mark.parametrize("existing", ["", "7"])
@pytest.mark.parametrize("response,api_exit", [("", 1), ("not JSON", 0)])
def test_failed_history_lookup_never_creates_or_edits_pr(tmp_path, existing, response, api_exit):
    result, calls = run_release_step(tmp_path, response=response, api_exit=api_exit, existing=existing)
    assert result.returncode != 0
    assert [call[0] for call in calls] == ["api"]
    assert not (tmp_path / "body.md").exists()


@pytest.mark.parametrize("history,ancestor", [(False, 0), (True, 1), (True, 0)])
def test_successful_lookup_preserves_empty_diverged_and_carried_ranges(tmp_path, history, ancestor):
    merged = {"number": 5, "merged_at": "2026-10-19T08:00:00Z",
              "head": {"sha": MERGED, "repo": {"full_name": "o/r"}}}
    response = json.dumps([[merged]] if history else [[]])
    result, calls = run_release_step(tmp_path, response=response, ancestor=ancestor)
    assert result.returncode == 0, result.stderr
    assert calls[-1][:2] == ["pr", "edit"]
    body = (tmp_path / "body.md").read_text()
    assert "new clean subject" in body
    carried = history and ancestor == 0
    assert ("zqcanary7731" not in body) == carried
    assert ("Commits since #5" in body) == carried
