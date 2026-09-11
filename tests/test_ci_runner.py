"""Exercise CI exit contracts with real scripts and controlled external tools.

The engine substitute runs only copies in a temporary fixture repository. It
never invokes a container engine, installs dependencies, or contacts a service.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

ENGINE = r"""
import os, pathlib, shutil, sys
args = sys.argv[1:]
if args[:2] == ["image", "inspect"]:
    if "--format" in args:
        print("amd64")
    raise SystemExit(0)
if args[0] != "run":
    raise SystemExit("unexpected engine operation")
if os.environ.get("FIXTURE_CONTAINER_VIEW"):
    # Model only the engine's filesystem visibility: copied source is visible,
    # while an external Git directory is absent unless explicitly mounted.
    source = pathlib.Path.cwd()
    view = pathlib.Path(os.environ["FIXTURE_CONTAINER_VIEW"])
    shutil.copytree(source, view, ignore=shutil.ignore_patterns(".ci-pixi"))
    gitfile = view / ".git"
    target = pathlib.Path(gitfile.read_text().removeprefix("gitdir: ").strip())
    exposed = False
    for index, argument in enumerate(args[:-1]):
        if argument != "-v":
            continue
        host, guest, options = args[index + 1].split(":", 2)
        if (pathlib.Path(host) == pathlib.Path(guest)
                and target.is_relative_to(host) and "ro" in options.split(",")):
            exposed = True
    if not exposed:
        gitfile.write_text("gitdir: " + str(view / "unmounted-metadata") + "\n")
    os.chdir(view)
command = args[args.index("bash") + 1:]
# Container PATH is already represented by the fixture environment. A host
# login shell would replace that controlled PATH with unrelated host tools.
if command[0] == "-lc":
    command[0] = "-c"
os.execve("/bin/bash", ["bash", *command], dict(os.environ))
"""

TOOL = r"""
import json, os, pathlib, sys
name = pathlib.Path(sys.argv[0]).name
args = sys.argv[1:]
if name == "pixi":
    if args[0] == "install":
        name = "pixi-install"
    elif args[0] == "run":
        args = args[1:]
        if args[:1] == ["--locked"]:
            args = args[1:]
        name, *args = args
    else:
        raise SystemExit("unexpected pixi operation")
if name == "python":
    if args[:1] == ["-m"]:
        name, *args = args[1:]
    elif args == ["ci/package_check.py", "install"]:
        name = "install"
    else:
        raise SystemExit("unexpected fixture Python command: " + repr(args))
if name in ("markdownlint-cli2", "markdownlint"):
    name = "markdownlint"
if name == "pip_audit":
    name = "pip-audit"
with open(os.environ["FIXTURE_CALLS"], "a") as file:
    file.write(json.dumps({"tool": name, "args": args}) + "\n")
if os.environ.get("FIXTURE_MISSING") == name:
    print("missing executable: " + name, file=sys.stderr)
    raise SystemExit(127)
if os.environ.get("FIXTURE_FAIL") == name:
    print("fixture failure: " + name, file=sys.stderr)
    raise SystemExit(8)
if name == "build":
    pathlib.Path("dist").mkdir(exist_ok=True)
    pathlib.Path("dist/fixture.whl").touch()
    pathlib.Path("dist/fixture.tar.gz").touch()
print("fixture success: " + name)
"""


@dataclass
class Runner:
    root: Path
    environment: dict[str, str]
    calls: Path

    def run(
        self, subset: str | None = None, **environment: str
    ) -> subprocess.CompletedProcess[str]:
        argv = ["/bin/bash", "scripts/run_ci_local.sh"]
        if subset is not None:
            argv.append(subset)
        return subprocess.run(
            argv,
            cwd=self.root,
            env={**self.environment, **environment},
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )

    def tools(self) -> list[str]:
        if not self.calls.exists():
            return []
        return [json.loads(line)["tool"] for line in self.calls.read_text().splitlines()]

    def track(self, name: str) -> None:
        subprocess.run(["git", "add", "--", name], cwd=self.root, check=True, capture_output=True)


@pytest.fixture
def runner(tmp_path: Path) -> Runner:
    root = tmp_path / "fixture repository with spaces"
    root.mkdir()
    for directory in ("scripts", "ci"):
        target = root / directory
        target.mkdir()
        for source in (ROOT / directory).glob("*"):
            if source.suffix in {".py", ".sh"}:
                shutil.copy2(source, target / source.name)
    (root / "README.md").write_text("# Valid documentation\n")
    (root / "pyproject.toml").write_text('[project]\nversion = "1.0.0"\n')
    (root / "pixi.toml").write_text('[workspace]\nversion = "1.0.0"\n')
    (root / "CHANGELOG.md").write_text("# Changes\n\n## [Unreleased]\n\n- A real change.\n")
    (root / "src" / "telemachy").mkdir(parents=True)
    (root / "src" / "telemachy" / "__init__.py").write_text('__version__ = "1.0.0"\n')
    (root / "example.yaml").write_text("enabled: true\n")
    (root / "justfile").write_text("default:\n    @echo fixture\n")
    subprocess.run(["git", "init", "-q"], cwd=root, check=True, capture_output=True)
    subprocess.run(["git", "add", "."], cwd=root, check=True, capture_output=True)
    binary = tmp_path / "bin"
    binary.mkdir()
    for name, body in [
        ("fixture-engine", ENGINE),
        *[
            (name, TOOL)
            for name in (
                "pixi",
                "markdownlint-cli2",
                "markdownlint",
                "gitleaks",
                "ruff",
                "mypy",
                "yamllint",
                "just",
                "twine",
                "npm",
                "python",
            )
        ],
    ]:
        executable = binary / name
        executable.write_text(f"#!{sys.executable}\n" + body)
        executable.chmod(0o755)
    (binary / "python3").symlink_to(sys.executable)
    home = tmp_path / "home"
    home.mkdir()
    calls = tmp_path / "calls.jsonl"
    return Runner(
        root,
        {
            "PATH": f"{binary}:/usr/bin:/bin",
            "HOME": str(home),
            "CONTAINER_ENGINE": str(binary / "fixture-engine"),
            "FIXTURE_CALLS": str(calls),
        },
        calls,
    )


@pytest.mark.parametrize(
    ("subset", "tool"),
    [
        ("markdownlint", "markdownlint"),
        ("security-dependency-scan", "pip-audit"),
        ("security-secrets-scan", "gitleaks"),
    ],
)
@pytest.mark.parametrize("failure", ["FIXTURE_FAIL", "FIXTURE_MISSING"])
def test_required_tool_failure_propagates(
    runner: Runner, subset: str, tool: str, failure: str
) -> None:
    result = runner.run(subset, **{failure: tool})
    assert result.returncode != 0, result.stdout
    assert tool in result.stderr
    assert "All CI checks passed" not in result.stdout


def test_failed_environment_install_never_falls_back_to_an_audit(runner: Runner) -> None:
    result = runner.run("security-dependency-scan", FIXTURE_FAIL="pixi-install")
    assert result.returncode != 0, result.stdout
    assert "pixi-install" in result.stderr
    assert "pip-audit" not in runner.tools()


def test_container_checks_preserve_the_host_environment(runner: Runner) -> None:
    environment = runner.root / ".pixi"
    environment.mkdir()
    marker = environment / "host-environment"
    marker.write_text("owned host environment\n")
    result = runner.run("markdownlint")
    assert result.returncode == 0, result.stdout + result.stderr
    assert marker.read_text() == "owned host environment\n"


def test_suppression_gate_rejects_a_tracked_silent_failure(runner: Runner) -> None:
    (runner.root / "scripts" / "broken.sh").write_text("#!/bin/bash\nfalse || true\n")
    runner.track("scripts/broken.sh")
    result = runner.run("forbid-suppressions")
    assert result.returncode != 0, result.stdout
    assert "scripts/broken.sh" in result.stdout + result.stderr


def test_symlink_gate_rejects_a_broken_tracked_link(runner: Runner) -> None:
    (runner.root / "missing-link").symlink_to("does-not-exist")
    runner.track("missing-link")
    result = runner.run("symlink-check")
    assert result.returncode != 0, result.stdout
    assert "missing-link" in result.stdout + result.stderr


def test_symlink_gate_accepts_a_valid_tracked_link(runner: Runner) -> None:
    (runner.root / "valid-link").symlink_to("README.md")
    runner.track("valid-link")
    assert runner.run("symlink-check").returncode == 0


@pytest.mark.parametrize("subset", [None, "all"])
def test_all_runs_the_required_checks(runner: Runner, subset: str | None) -> None:
    result = runner.run(subset)
    assert result.returncode == 0, result.stdout + result.stderr
    assert {
        "ruff",
        "mypy",
        "yamllint",
        "markdownlint",
        "pytest",
        "gitleaks",
        "pip-audit",
        "bandit",
        "build",
        "twine",
        "install",
    } <= set(runner.tools())


def test_all_stops_before_later_checks_on_failure(runner: Runner) -> None:
    result = runner.run("all", FIXTURE_FAIL="markdownlint")
    assert result.returncode != 0
    assert "markdownlint" in runner.tools()
    assert "build" not in runner.tools()


@pytest.mark.parametrize(
    "subset", ["markdownlint", "security-dependency-scan", "security-secrets-scan"]
)
def test_successful_checks_still_pass(runner: Runner, subset: str) -> None:
    result = runner.run(subset)
    assert result.returncode == 0, result.stdout + result.stderr


def test_secrets_scan_requests_redacted_sarif_without_success_override(runner: Runner) -> None:
    assert runner.run("security-secrets-scan").returncode == 0
    call = json.loads(runner.calls.read_text())
    assert call["tool"] == "gitleaks"
    assert call["args"] == [
        "detect",
        "--no-banner",
        "--redact",
        "--source",
        ".",
        "--report-format",
        "sarif",
        "--report-path",
        "gitleaks.sarif",
    ]


def test_worktree_metadata_remains_available_in_the_container_view(runner: Runner) -> None:
    metadata = runner.root.parent / "separate git metadata"
    (runner.root / ".git").rename(metadata)
    (runner.root / ".git").write_text(f"gitdir: {metadata}\n")
    result = runner.run(
        "markdownlint", FIXTURE_CONTAINER_VIEW=str(runner.root.parent / "container view")
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert runner.tools() == ["markdownlint"]


def test_secrets_scan_preserves_repository_configuration(runner: Runner) -> None:
    (runner.root / ".gitleaks.toml").write_text("[extend]\nuseDefault = true\n")
    assert runner.run("security-secrets-scan").returncode == 0
    call = json.loads(runner.calls.read_text())
    assert "--config" in call["args"]
    assert call["args"][call["args"].index("--config") + 1] == ".gitleaks.toml"
