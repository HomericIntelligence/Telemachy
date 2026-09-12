"""Shared installed-wheel and release dry-run gates; never publishes packages."""

from __future__ import annotations

import argparse
import ast
import re
import subprocess
import sys
import tempfile
import tomllib
import venv
from pathlib import Path


def release() -> None:
    project = tomllib.loads(Path("pyproject.toml").read_text())
    version = project["project"]["version"]
    init_version = None
    for node in ast.walk(ast.parse(Path("src/telemachy/__init__.py").read_text())):
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "__version__" for target in node.targets
        ):
            init_version = ast.literal_eval(node.value)
    if version != init_version:
        raise ValueError("pyproject.toml and __init__.py versions disagree")
    section = re.search(
        rf"^## \[(?:Unreleased|{re.escape(version)})\][^\n]*\n(.*?)(?=^## \[|\Z)",
        Path("CHANGELOG.md").read_text(),
        re.MULTILINE | re.DOTALL,
    )
    if section is None or not section.group(1).strip():
        raise ValueError("CHANGELOG.md has no non-empty stageable release section")
    print(f"Release dry-run OK: version {version}, stageable changelog; no publish performed.")


def install() -> None:
    wheels = list(Path("dist").glob("*.whl"))
    if len(wheels) != 1:
        raise ValueError("The install check requires exactly one built wheel in dist")
    wheel = wheels[0].resolve()
    manifest = tomllib.loads(Path("pixi.toml").read_text())
    requirements = []
    for name, spec in manifest["pypi-dependencies"].items():
        if name == "telemachy":
            continue
        version = spec if isinstance(spec, str) else spec.get("version", "*")
        requirements.append(name if version in {"*", "", None} else f"{name}{version}")
    with tempfile.TemporaryDirectory(prefix="telemachy-install-") as directory:
        root = Path(directory)
        venv.EnvBuilder(with_pip=True).create(root / "venv")
        python = str(root / "venv" / "bin" / "python")
        # Match the supported pixi-managed consumption contract: runtime
        # dependencies are declared in pixi.toml, outside wheel metadata.
        subprocess.run([python, "-m", "pip", "install", *requirements], cwd=root, check=True)
        subprocess.run(
            [python, "-m", "pip", "install", "--no-deps", str(wheel)], cwd=root, check=True
        )
        subprocess.run(
            [
                python,
                "-I",
                "-c",
                "import telemachy; import telemachy.cli; print(telemachy.__version__)",
            ],
            cwd=root,
            check=True,
        )
        subprocess.run([python, "-I", "-m", "telemachy.cli", "--help"], cwd=root, check=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("check", choices=("install", "release"))
    args = parser.parse_args()
    try:
        (install if args.check == "install" else release)()
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        print(f"Package check failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
