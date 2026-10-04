"""Run a composite action's shell step for real, with its external commands faked.

YAML assertions can say what a step's script contains, not what it executes. The
tests that need the second run the script under bash with `aws`, `docker`,
`curl` and `argocd` replaced by stubs that record every call, then assert on the
recorded argv. The stubs are written per test, so nothing reaches a real cloud,
cluster or API.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

# Field and record separators for the call log. Arguments can hold spaces and
# newlines (a JSON policy document does), so neither can delimit them.
FIELD = "\x1f"
RECORD = "\x1e"


class Stubs:
    """Fake commands on PATH. Each one appends its argv to a log, then runs its body."""

    def __init__(self, root: Path) -> None:
        self.bin = root / "bin"
        self.bin.mkdir()
        self.log = root / "calls.log"
        self.log.touch()

    def add(self, name: str, body: str = "exit 0") -> None:
        script = self.bin / name
        script.write_text(
            "#!/usr/bin/env bash\n"
            f"{{ printf '%s\\037' {name} \"$@\"; printf '\\036'; }} >> \"$STUB_LOG\"\n"
            f"{body}\n",
            encoding="utf-8",
        )
        script.chmod(0o755)

    def calls(self, name: str | None = None) -> list[list[str]]:
        records = self.log.read_text(encoding="utf-8").split(RECORD)
        argvs = [record.split(FIELD)[:-1] for record in records if record]
        return [argv for argv in argvs if name is None or argv[0] == name]

    def run(self, script: str, env: dict[str, str], cwd: Path) -> subprocess.CompletedProcess[str]:
        """Run `script` the way a composite `shell: bash` step runs it."""
        path = cwd / "step.sh"
        path.write_text(script, encoding="utf-8")
        bash = shutil.which("bash")
        assert bash, "bash is required to run composite action steps"
        return subprocess.run(
            [bash, "--noprofile", "--norc", "-eo", "pipefail", str(path)],
            env={
                "PATH": f"{self.bin}{os.pathsep}{os.environ['PATH']}",
                "HOME": str(cwd),
                "STUB_LOG": str(self.log),
                **env,
            },
            cwd=cwd,
            capture_output=True,
            text=True,
            check=False,
        )


@pytest.fixture
def stubs(tmp_path: Path) -> Stubs:
    return Stubs(tmp_path)
