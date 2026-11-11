"""Canonical local/hosted CI checks, executed inside the declared CI image."""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import tempfile
from pathlib import Path

CHECKS = (
    "forbid-suppressions",
    "pixi-check",
    "lint",
    "markdownlint",
    "justfile-check",
    "symlink-check",
    "unit-tests",
    "integration-tests",
    "schema-validation",
    "security-secrets-scan",
    "security-dependency-scan",
    "security-sast-scan",
    "deps-version-sync",
    "build",
    "package",
    "install",
    "release",
)


def run(*args: str) -> None:
    subprocess.run(args, check=True)


def tracked(*patterns: str) -> list[str]:
    result = subprocess.run(
        ["git", "ls-files", "-z", "--", *patterns],
        check=True,
        stdout=subprocess.PIPE,
    )
    return [name.decode() for name in result.stdout.split(b"\0") if name]


def suppressions() -> None:
    patterns = (
        re.compile(r"\|\|\s*true\b"),
        re.compile(r"^\s*set\s+\+e(?:\s|;|$)"),
        re.compile(r"^\s*continue-on-error:\s*true\s*$"),
    )
    errors = []
    for name in tracked(
        "*.sh",
        "*.bash",
        "*.yml",
        "*.yaml",
        "*.hcl",
        "*Dockerfile*",
        "*Containerfile*",
        "justfile",
        "**/justfile",
        "Justfile",
        "**/Justfile",
    ):
        for number, line in enumerate(Path(name).read_text().splitlines(), 1):
            if not line.lstrip().startswith("#") and any(
                pattern.search(line) for pattern in patterns
            ):
                errors.append(f"{name}:{number}: silent failure suppression")
    if errors:
        raise ValueError("\n".join(errors))


def symlinks() -> None:
    result = subprocess.run(
        ["git", "ls-files", "--stage", "-z"],
        check=True,
        stdout=subprocess.PIPE,
    )
    for entry in result.stdout.split(b"\0"):
        if entry.startswith(b"120000 "):
            name = entry.split(b"\t", 1)[1].decode()
            path = Path(name)
            if not path.is_symlink() or not path.exists():
                raise ValueError(f"{name}: tracked symlink is missing or has a broken target")


class Checks:
    def __init__(self) -> None:
        self.installed = False

    def install_environment(self) -> None:
        if not self.installed:
            run("pixi", "install", "--locked")
            self.installed = True

    def pixi(self, *args: str) -> None:
        self.install_environment()
        run("pixi", "run", "--locked", *args)

    def check(self, name: str) -> None:
        print(f"==> {name}", flush=True)
        if name == "forbid-suppressions":
            suppressions()
        elif name in {"pixi-check", "deps-version-sync"}:
            self.install_environment()
        elif name == "lint":
            self.pixi("ruff", "check", "src", "tests", "ci")
            self.pixi("mypy", "src/telemachy", "--ignore-missing-imports")
            self.pixi("yamllint", "-c", ".yamllint.yaml", *tracked("*.yml", "*.yaml"))
        elif name == "markdownlint":
            files = tracked("*.md")
            if not files:
                raise ValueError("No tracked Markdown files were selected")
            run("markdownlint-cli2", *files)
        elif name == "justfile-check":
            run("just", "--evaluate")
        elif name == "symlink-check":
            symlinks()
        elif name == "unit-tests":
            # Preserve the existing full-suite coverage gate; the integration
            # context below also reports its selected service-boundary tests.
            self.pixi(
                "pytest",
                "--tb=short",
                "-q",
                "--cov=telemachy",
                "--cov-report=term-missing",
                "--cov-fail-under=75",
            )
        elif name == "integration-tests":
            self.pixi("pytest", "-m", "integration", "tests/integration", "--tb=short", "-q")
        elif name == "schema-validation":
            self.pixi("python", "-m", "telemachy.cli", "schema", "-o", "telemachy-schema.json")
        elif name == "security-secrets-scan":
            config = ["--config", ".gitleaks.toml"] if Path(".gitleaks.toml").is_file() else []
            run(
                "gitleaks",
                "detect",
                "--no-banner",
                "--redact",
                "--source",
                ".",
                "--report-format",
                "sarif",
                "--report-path",
                "gitleaks.sarif",
                *config,
            )
        elif name == "security-dependency-scan":
            self.install_environment()
            # Exclude only this unpublished project, not every editable. All
            # other installed packages (including packaging tools) are frozen.
            # Strict audit rejects unsupported entries and collection errors.
            with tempfile.TemporaryDirectory(prefix="telemachy-audit-") as directory:
                requirements = Path(directory) / "requirements.txt"
                with requirements.open("w") as output:
                    subprocess.run(
                        [
                            "pixi",
                            "run",
                            "--locked",
                            "python",
                            "-m",
                            "pip",
                            "freeze",
                            "--all",
                            "--exclude",
                            "telemachy",
                        ],
                        stdout=output,
                        check=True,
                    )
                self.pixi(
                    "python",
                    "-m",
                    "pip_audit",
                    "--strict",
                    "--no-deps",
                    "--disable-pip",
                    "--requirement",
                    str(requirements),
                    "--progress-spinner",
                    "off",
                )
            run("npm", "audit", "--prefix", "/opt/ci-tools", "--omit=dev")
        elif name == "security-sast-scan":
            self.pixi(
                "python",
                "-m",
                "bandit",
                "-ll",
                "--ini",
                ".bandit",
                "-r",
                "src/telemachy",
                "-f",
                "json",
                "-o",
                "bandit.json",
            )
        elif name == "build":
            self.pixi("python", "-m", "build", "--no-isolation")
        elif name == "package":
            files = sorted(str(path) for path in Path("dist").glob("*"))
            if not files:
                raise ValueError("dist is empty; run the build check first")
            self.pixi("twine", "check", *files)
        elif name == "install":
            self.pixi("python", "ci/package_check.py", "install")
        elif name == "release":
            # Same dry-run body as the hosted release context; never publishes.
            run("python3", "ci/package_check.py", "release")
        else:
            raise ValueError(f"Unknown CI subset: {name}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("subset", choices=("all", *CHECKS), nargs="?", default="all")
    args = parser.parse_args()
    checks = Checks()
    try:
        for name in CHECKS if args.subset == "all" else (args.subset,):
            checks.check(name)
    except subprocess.CalledProcessError as error:
        return error.returncode if error.returncode > 0 else 1
    except (OSError, ValueError) as error:
        print(f"CI failed: {error}", file=sys.stderr)
        return 1
    print(f"All CI checks passed ({args.subset}).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
