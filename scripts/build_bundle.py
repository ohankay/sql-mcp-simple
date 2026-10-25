"""Build the offline Windows x64 install bundle: release/sql-mcp-simple-windows-x64.zip

    pip install uv && python scripts/build_bundle.py

The zip contains the sql-mcp-simple wheel, every dependency wheel for Windows x64 / CPython
3.10-3.14 (so installation works without internet), install.cmd, the agent install guide
and VS Code templates.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PYTHON_VERSIONS = ("3.10", "3.11", "3.12", "3.13", "3.14")
OUT = ROOT / "release" / "sql-mcp-simple-windows-x64.zip"
TOP = "sql-mcp-simple"


def _windows_requirements(version: str, out: Path) -> Path:
    """Pin the full dependency tree as resolved ON WINDOWS for this Python version.

    pip evaluates markers such as ``sys_platform == "win32"`` against the build machine, so a plain
    ``pip download --platform win_amd64`` silently drops Windows-only packages (e.g. pywin32).
    uv resolves for the target platform instead.
    """
    req = out / f"requirements-{version}.txt"
    subprocess.run(["uv", "pip", "compile", str(ROOT / "pyproject.toml"), "--python-platform",
                    "x86_64-pc-windows-msvc", "--python-version", version, "--no-header",
                    "--no-annotate", "-q", "-o", str(req)], check=True)
    return req


def _pip(*args: str) -> None:
    subprocess.run([sys.executable, "-m", "pip", *args, "--disable-pip-version-check", "-q"], check=True)


def build() -> Path:
    with tempfile.TemporaryDirectory() as tmp:
        wheelhouse = Path(tmp) / "wheelhouse"
        wheelhouse.mkdir()
        _pip("wheel", str(ROOT), "--no-deps", "-w", str(wheelhouse))
        for version in PYTHON_VERSIONS:
            req = _windows_requirements(version, Path(tmp))
            _pip("download", "-r", str(req), "--no-deps", "--platform", "win_amd64", "--python-version",
                 version, "--only-binary=:all:", "-d", str(wheelhouse))

        OUT.parent.mkdir(exist_ok=True)
        if OUT.exists():
            OUT.unlink()
        files: list[tuple[Path, str]] = [
            (ROOT / "bundle" / "KURULUM_REHBERI.md", "KURULUM_REHBERI.md"),
            (ROOT / "bundle" / "install.cmd", "install.cmd"),
            (ROOT / "README.md", "README.md"),
            (ROOT / "LICENSE", "LICENSE"),
        ]
        files += [(p, f"templates/{p.name}") for p in sorted((ROOT / "bundle" / "templates").iterdir())]
        files += [(p, f"wheelhouse/{p.name}") for p in sorted(wheelhouse.iterdir())]
        with zipfile.ZipFile(OUT, "w", zipfile.ZIP_DEFLATED) as zf:
            for src, arc in files:
                zf.write(src, f"{TOP}/{arc}")
    return OUT


if __name__ == "__main__":
    path = build()
    print(f"{path} ({path.stat().st_size / 1_048_576:.1f} MB)")
